pipeline {
    agent any

    environment {
        AWS_REGION       = 'ap-south-1'
        ECR_REGISTRY     = '463356420488.dkr.ecr.ap-south-1.amazonaws.com'
        ECR_REPO         = 'agent-platform'
        IMAGE_TAG        = "${env.GIT_COMMIT?.take(8) ?: 'latest'}"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('ECR Login') {
            steps {
                sh '''
                    aws ecr get-login-password --region $AWS_REGION \
                      | docker login --username AWS --password-stdin $ECR_REGISTRY
                '''
            }
        }

        stage('Build Images') {
            parallel {
                stage('Backend') {
                    steps {
                        sh '''
                            docker build \
                              -f Dockerfile.backend \
                              -t $ECR_REGISTRY/$ECR_REPO:backend-$IMAGE_TAG \
                              -t $ECR_REGISTRY/$ECR_REPO:backend-latest \
                              .
                        '''
                    }
                }
                stage('Frontend') {
                    steps {
                        sh '''
                            docker build \
                              -f Dockerfile.frontend \
                              -t $ECR_REGISTRY/$ECR_REPO:frontend-$IMAGE_TAG \
                              -t $ECR_REGISTRY/$ECR_REPO:frontend-latest \
                              .
                        '''
                    }
                }
            }
        }

        stage('Push Images') {
            parallel {
                stage('Push Backend') {
                    steps {
                        sh '''
                            docker push $ECR_REGISTRY/$ECR_REPO:backend-$IMAGE_TAG
                            docker push $ECR_REGISTRY/$ECR_REPO:backend-latest
                        '''
                    }
                }
                stage('Push Frontend') {
                    steps {
                        sh '''
                            docker push $ECR_REGISTRY/$ECR_REPO:frontend-$IMAGE_TAG
                            docker push $ECR_REGISTRY/$ECR_REPO:frontend-latest
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            sh '''
                docker rmi $ECR_REGISTRY/$ECR_REPO:backend-$IMAGE_TAG || true
                docker rmi $ECR_REGISTRY/$ECR_REPO:backend-latest || true
                docker rmi $ECR_REGISTRY/$ECR_REPO:frontend-$IMAGE_TAG || true
                docker rmi $ECR_REGISTRY/$ECR_REPO:frontend-latest || true
            '''
        }
        success {
            echo "Pushed backend and frontend images tagged ${IMAGE_TAG} to ${ECR_REGISTRY}/${ECR_REPO}"
        }
        failure {
            echo 'Build or push failed.'
        }
    }
}
